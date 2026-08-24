# 1. k8s的numa研究及优化

## 1.1. 目标

在研究kubevirt的过程中，我们发现，一个虚拟机的产生是基于pod的，反之 虚拟机的资源配置是基于pod的资源的。在虚拟机的创建过程中，我们发现针对拥有pci-passthrougth类型的gpu，在为pod分配资源时，无法将pod的cpu指定到与gpu的numa一致的节点上。

比如我们的gpu7所在的numa节点为7，如果我们分配10个固定cpu，那么这10个cpu的numa节点无法与gpu7对应上，我们目标解决该问题。

但考虑到文章的长度，在此我们只分析pod是如何被分配numa节点的，而虚拟机如何使用pod的numa信息则会放到下一部分，因为我们还没有解决这个问题。

## 1.2. 结论

先说结论：

对于pci-passthrough形式的gpu，我们只需要简单的修改下gpu信息上报的daemon，让它们将当前没有上报的numa节点信息上报到kubelet层，然后kubelet即可正常分配。

## 1.3. 过程

我们从kubelet源码开始分析，来看看kubelet是如何对numa进行匹配，以及我们需要修改什么才能使得gpu也上报自己的numa信息。

### 1.3.1. kubelet

参考文档：

- <https://my.oschina.net/jxcdwangtao/blog/1803036> [kubelet deviceManager实现]
- <https://my.oschina.net/jxcdwangtao/blog/1797047> [kubelet deviceManager接收上报信息的实现]
- <https://my.oschina.net/jxcdwangtao/blog/1793656> [devicePlugin上报gpu信息]
- <https://devopscube.com/kubernetes-architecture-explained/#1-kubelet> [k8s的各个组件的运行方式]

#### 1.3.1.1. run方法

入口位置： `kubernetes/pkg/kubelet/server`的`run`方法

该方法会启动kubelet程序，并使其作为守护进程。 而它的动作与众多controller的实现类似，它会不断的监控着kubernets服务端的变化，如果有pod被scheduler到本节点，它就会去执行相应的方法。

#### 1.3.1.2. containerManager对象

我们需要了解的是，kubelet是由众多manager来实现的，而deviceManager只是containerManager的一个子Manager。而containerManager主要由以下几个子Manager组成：

- topologyManager: 负责一个pod及其container的numa节点的匹配
- cpuManager: 负责cpu的分配
- memoryManager: 负责mem的分配
- deviceManager：负责device的分配

入口位置： `kubernetes/pkg/kubelet/cm/container_manager_linux.go`的`NewContainerManager`函数，由于代码太长，我们只文字描述下它的功能。

- 获取到当前节点的Cgroup配置，并且存储到subsystems变量中
- 获取到当前节点的swap是否开启，并且存储到isSwapOn变量中。如果swapOn，则需要关闭，否则kubelet无法启动。
- 遍历节点的Capacity，主要是cpu、内存和大页，然后存储到internalCapacity中
- 获取节点的pidlimits，也存储到internalCapacity中
- 根据启动参数或者默认的cgroup选项，启动cgroupManager，这也是众多Manager之一
- 根据启动参数或者默认的cgroup选项，启动qosContainerManager
- 根据containerManagerImpl生成cm(containerManager)，将上面的多个作为结构体的参数。
- 添加cm.topologyManager，这个manager主要是记录了主机的cpu、内存和numa的拓扑信息。并且它生成了一个scope内存存储的机制，该scope可以被下面的manager存储。
- 添加cm.deviceManager, deviceManager会生成将topologyManager对象作用参数，将scope记录到自己的结构体变量中。
- 反之，topologyManager会在deviceManager生成后，使用它添加一个HintProvider，这是在DeviceManager有topo信息时，进行topo匹配时的。
- 将函数的参数kubeclient传入到cm对象中。 是的,kubelet它只是一个守护程序，它也是需要从k8s服务端获取信息的。
- 添加cm.cpuManager, 如同deviceManager一样，也会将topologyManager作为参数获取到scope。 反之cpuManager也入到topologyManager的hint中。
- 添加cm.memoryManager，如cpuManager。
- 对象生成完毕

各个Manager之间的关系如下。

![picture 0](assets/k8s%E7%9A%84numa%E7%A0%94%E7%A9%B6%E5%8F%8A%E4%BC%98%E5%8C%96-1741330532052.png)  

但它们的关系其实挺复杂的。

- topologyManager生成对象后会包含一个scope变量，用来存储pod的numa信息。而其它三个Manager在生成时就将topologyManager作为参数，以获取到scope。相当于共享一个变量，所以当三个manager操作scope时，会请求内存锁。
- 而三个Manager每当生成完成后，topologyManager使用AddHintProvider方法，将它们再存入到自己的provider中，这样就可以使用它们的numa计算方法。

#### 1.3.1.3. 总结

这里我们分析了containerManager和它的四个子Manager的运行机制及逻辑关系。

### 1.3.2. topologyManager

#### 1.3.2.1. admit方法

它的调用逻辑是这样的

`kubelet.Run --> kubelet.syncLoop --> kubelet.syncLoopIteration --> kubelet.HandlePodAdditions --> kubelet.canAdmitPod --> topologyManager.scope_pod.Admit`

最终会调用到topologymanager目录的scope_pod.go的Admit方法。当然，除了containerManager外，还有许多其它的Manager也会有Admit方法。

```go
func (s *podScope) Admit(pod *v1.Pod) lifecycle.PodAdmitResult {
    bestHint, admit := s.calculateAffinity(pod)
    klog.InfoS("Best TopologyHint", "bestHint", bestHint, "pod", klog.KObj(pod))
    if !admit {
        metrics.TopologyManagerAdmissionErrorsTotal.Inc()
        return admission.GetPodAdmitResult(&TopologyAffinityError{})
    }

    for _, container := range append(pod.Spec.InitContainers, pod.Spec.Containers...) {
        klog.InfoS("Topology Affinity", "bestHint", bestHint, "pod", klog.KObj(pod), "containerName", container.Name)
        s.setTopologyHints(string(pod.UID), container.Name, bestHint)

        err := s.allocateAlignedResources(pod, &container)
        if err != nil {
            metrics.TopologyManagerAdmissionErrorsTotal.Inc()
            return admission.GetPodAdmitResult(err)
        }
    }
    if IsAlignmentGuaranteed(s.policy) {
        // increment only if we know we allocate aligned resources.
        klog.V(4).InfoS("Resource alignment at pod scope guaranteed", "pod", klog.KObj(pod))
        metrics.ContainerAlignedComputeResources.WithLabelValues(metrics.AlignScopePod, metrics.AlignedNUMANode).Inc()
    }
    return admission.GetPodAdmitResult(nil)
}

func (s *podScope) accumulateProvidersHints(pod *v1.Pod) []map[string][]TopologyHint {
    var providersHints []map[string][]TopologyHint

    for _, provider := range s.hintProviders {
        // Get the TopologyHints for a Pod from a provider.
        hints := provider.GetPodTopologyHints(pod)
        providersHints = append(providersHints, hints)
        klog.InfoS("TopologyHints", "hints", hints, "pod", klog.KObj(pod))
    }
    return providersHints
}

func (s *podScope) calculateAffinity(pod *v1.Pod) (TopologyHint, bool) {
    providersHints := s.accumulateProvidersHints(pod)
    bestHint, admit := s.policy.Merge(providersHints)
    klog.InfoS("PodTopologyHint", "bestHint", bestHint, "pod", klog.KObj(pod))
    return bestHint, admit
}
```

我们可以通过代码，总结几大步骤：

- 来了进行accumulateProvidersHints，根据cpuManager、memManager和deviceManager的GetPodTopologyHints算出各自的hints
- 然后进行Merge方法，通过这个方法名我们也能知道，它是要进行一个合并，算出这几个hints的最优集，最后来提供bestHint。（但不同的numa策略应该有不同的算法，这个以后再说）
- 好，选出bestHint后，那么就是将信息setTopologyHints到pod中。
- 最后allocateAlignedResources到各个manager中分配资源。

注意`s.setTopologyHints(string(pod.UID), container.Name, bestHint)`这行代码，它为pod在当前的scope的中添加了一个bestHint。

#### 1.3.2.2. cpuManager的GetPodTopologyHints

这个在cpuManager的policy_static.go中。

一般情况下，本机的kubelet会指定一种cpu策略，而当它作为kubevirt节点时，它被建议设置为了static（cpu-manager-policy）。这样才能进行cpu的固定分配。 而当为none或options时，则没有GetPodTopologyHints的算法实现，或者会被返回nil。

这里我们将不会分析这个函数，而是分析被它调用的generateCPUTopologyHints函数

##### 1.3.2.2.1. numa掩码

在具体分析之前，我们先看下Numa的掩码是怎么算的。

假设你有一个系统，其中有 8 个 NUMA 节点（编号为 0 到 7）。每个 NUMA 节点可以用一个位来表示：

- 节点 0：`00000001`
- 节点 1：`00000010`
- 节点 2：`00000100`
- 节点 3：`00001000`
- 节点 4：`00010000`
- 节点 5：`00100000`
- 节点 6：`01000000`
- 节点 7：`10000000`

那个多个节点可以使用掩码相加的方式进行表示。 比如00000011表示的是节点1、0， 10000001表示的是节点7、0。

在接下来的分析中，我们会看到numa会遍历（或叫穷举），组成不同的组合，直到11111111。那么将会有255个组合，实际上也就是2^8个

##### 1.3.2.2.2. generateCPUTopologyHints方法

该方法仍在policy_static.go中

```go
func (p *staticPolicy) generateCPUTopologyHints(availableCPUs cpuset.CPUSet, reusableCPUs cpuset.CPUSet, request int) []topologymanager.TopologyHint {
    // Initialize minAffinitySize to include all NUMA Nodes.
    minAffinitySize := p.topology.CPUDetails.NUMANodes().Size()

    // Iterate through all combinations of numa nodes bitmask and build hints from them.
    hints := []topologymanager.TopologyHint{}
    bitmask.IterateBitMasks(p.topology.CPUDetails.NUMANodes().List(), func(mask bitmask.BitMask) {
        // First, update minAffinitySize for the current request size.
        cpusInMask := p.topology.CPUDetails.CPUsInNUMANodes(mask.GetBits()...).Size()
        if cpusInMask >= request && mask.Count() < minAffinitySize {
            minAffinitySize = mask.Count()
        }

        // Then check to see if we have enough CPUs available on the current
        // numa node bitmask to satisfy the CPU request.
        numMatching := 0
        for _, c := range reusableCPUs.List() {
            // Disregard this mask if its NUMANode isn't part of it.
            if !mask.IsSet(p.topology.CPUDetails[c].NUMANodeID) {
                return
            }
            numMatching++
        }

        // Finally, check to see if enough available CPUs remain on the current
        // NUMA node combination to satisfy the CPU request.
        for _, c := range availableCPUs.List() {
            if mask.IsSet(p.topology.CPUDetails[c].NUMANodeID) {
                numMatching++
            }
        }

        // If they don't, then move onto the next combination.
        if numMatching < request {
            return
        }

        // Otherwise, create a new hint from the numa node bitmask and add it to the
        // list of hints.  We set all hint preferences to 'false' on the first
        // pass through.
        hints = append(hints, topologymanager.TopologyHint{
            NUMANodeAffinity: mask,
            Preferred:        false,
        })
    })

    // Loop back through all hints and update the 'Preferred' field based on
    // counting the number of bits sets in the affinity mask and comparing it
    // to the minAffinitySize. Only those with an equal number of bits set (and
    // with a minimal set of numa nodes) will be considered preferred.
    for i := range hints {
        if p.options.AlignBySocket && p.isHintSocketAligned(hints[i], minAffinitySize) {
            hints[i].Preferred = true
            continue
        }
        if hints[i].NUMANodeAffinity.Count() == minAffinitySize {
            hints[i].Preferred = true
        }
    }

    return hints
}
```

注： 下面的解析中，numa表示单个numa,numas表示一个或多个组合, mask则是上一小节的numa使用mask的组合方式, nodeN的表示方式则是具体的numa节点。

在分析代码之前，我们假设我们有16个cpu，要被分配到96核8节点的cpu上，每个节点12个核。并且假设cpu0被作为了系统保留，那么availableCpus参数将只会剩下95个核心。request则是要请求的cpu的数量，16个。

- 先初始化一个minAffinitySize，这个变量表示我们的请求将最少要使用多少个numa节点，初始化值为8个。注意，这个值会在后期会变动。
- 定义一个包含多个TopologyHint{}的hints变量
- 组合遍历numa节点，也就是前一小节我们提到的numa mask的遍历，并且作为参数传入临时函数中。
  - 获取cpusInMask，也就当前numas中共包含多少个cpu。
  - `cpuInMask > request &&mask.Count < minAffinitySize`: 如果当前遍历的numas的cpu数量超过了请求的cpu数量，并且mask的数量小于当前minAffinitySize的值，那么minAffinitySize被赋新值
  - 初始化numMatching为0，该变量表示我们是否能在当前的numas中找到足够使用的cpu。比如我们我们遍历node0和node1，但这两个节点只有15个cpu可用。
  - 遍历reusableCPUS，这个参数一般会在pod在当前节点升级时使用，表示当前pod是否使用了这些节点中的cpu，这个先不用管。
  - 遍历availableCpus，如果cpu属于当前numa，那么numMatching+1
  - 最后判断numMattching是否小于request。 如果小于，那么当前遍历的到numas所包含的cpu数量不满足request请求的数量，退出当前循环继续下一循环。否则，则表示当前遍历到的numas所包含的cpu数量满足了request请求的数量，继续。 （会发生大于的情况，当前numas所包含的cpu数量超过了请求，这也是满足的）
  - 添加一个TopologyHint对到hints中，注意这时候的Preferred值为false。
- 再遍历hints的结果。
  - 如果当前的staticPolicy设置了Socket的亲近性，那么设置当前hint的Preferred为true,表示该hints会被"喜欢"也就是优先使用。但要注意，在这个判断里面，也会看它们是否等于minAffinitySize。
  - 如果当前hint所选中的numa节点数量等于minAffinitySize，那么它也会是Preferred为true，优先被使用。

---

- q: socket亲近性到底开不开？
  - a: 从代码中来看，这是一个非强制性的。如果两个numa跨了socket，它仍然会被设置为true。因为它并没有说在不满足条件的情况下退出循环，而是会继续进行下一步的判断。所以如果选中了node3+node4为第一个hints，那么它仍然会被设置为true。

##### 1.3.2.2.3. 手动算一下

那么我们根据上面的算法，假设我们8个numa节点，每个节numa点仍然是12个cpu。其中node0被占用了4个cpu，node1被占用了6个（两个节点剩余14个cpu），其它node没有被占用的cpu。

- 1、请求了10个cpu
  - node0只剩了8个，node1只剩了6个，都不满足，那么node2会被选中并放到hints变量的第一个，那么会选中node2会被优先使用。
- 2、请求了14个cpu
  - 巧了，正好是node0+node1的剩余。但node0、node1单独都不满足，node2以后也不满足，因为超过了单个节点的数量。
  - 那么就会看组合: node0+node1满足条件，node1+node2也满足，直到node0+...+node7都满足，那好，全部拿出来。
  - node0+node1是不是相同socket？是，好，喜欢； node2+node3是不是相同socket？是，好，喜欢； node3+node4是不是相同socket？不是，但仍然可以。
  - node0+node1+node2+node3不是相同socket？是，但你不是最小集。
- 3、请求了15个cpu
  - node0+node1=14?no, node1+node2=18?yes, node2+node3=24?yes，node3+node4=24?yes

最终，我们可以得出一个结论，numa的hint算法会在一堆组合中选出：满足cpu请求数量，numa数量最小的节点组合，并且优先使用排在最前的节点。

##### 1.3.2.2.4. 怎么才能均匀的分配（放弃）

放弃这一小节，后面我们可以通过设置kubelet的方式来进行均匀分配。

当我们将一个8卡节点分配给不同用户使用时，我们希望用户不论是使用几卡，所对应的cpu数量均是一样的。

```yaml
NUMA:                     
  NUMA node(s):           8
  NUMA node0 CPU(s):      0-5,48-53
  NUMA node1 CPU(s):      6-11,54-59
  NUMA node2 CPU(s):      12-17,60-65
  NUMA node3 CPU(s):      18-23,66-71
  NUMA node4 CPU(s):      24-29,72-77
  NUMA node5 CPU(s):      30-35,78-83
  NUMA node6 CPU(s):      36-41,84-89
  NUMA node7 CPU(s):      42-47,90-95
```

比如，当前节点是96核，我们需要设置第个Numa节点保留1个cpu，即0、6、12、18、24、30、36、42 cpu将会设置为保留。那么每个node剩余cpu将会是11个，那么会建议将每张显卡对应的cpu为11个，这样才能保证显卡对应的numa节点选取最合适的，并且不会产生不均匀的情况。

如果我们只设置0、6、12、18为保留,使用到node3+node4时仍然会产生11+11的效果。但node4+node5就会产生12+10的效果，每个显卡被分配的cpu会不均匀。
如果我们只设置0、6、12、18为保留,并且每个显卡使用cpu为10个。使用到node0+node1时就会发生11+9的情况。使用node4+node5会发生12+8的情况。

#### 1.3.2.3. deviceManager的GetPodTopologyHints

##### 1.3.2.3.1. GetPodTopologyHints方法

```go
func (m *ManagerImpl) GetPodTopologyHints(pod *v1.Pod) map[string][]topologymanager.TopologyHint {
    // Garbage collect any stranded device resources before providing TopologyHints
    m.UpdateAllocatedDevices()

    deviceHints := make(map[string][]topologymanager.TopologyHint)
    accumulatedResourceRequests := m.getPodDeviceRequest(pod)

    m.mutex.Lock()
    defer m.mutex.Unlock()
    for resource, requested := range accumulatedResourceRequests {
        // Only consider devices that actually contain topology information.
        if aligned := m.deviceHasTopologyAlignment(resource); !aligned {
            klog.InfoS("Resource does not have a topology preference", "resourceName", resource, "pod", klog.KObj(pod), "request", requested)
            deviceHints[resource] = nil
            continue
        }

        // Short circuit to regenerate the same hints if there are already
        // devices allocated to the Pod. This might happen after a
        // kubelet restart, for example.
        allocated := m.podDevices.podDevices(string(pod.UID), resource)
        if allocated.Len() > 0 {
            if allocated.Len() != requested {
                klog.InfoS("Resource already allocated to pod with different number than request", "resourceName", resource, "pod", klog.KObj(pod), "request", requested, "allocated", allocated.Len())
                deviceHints[resource] = []topologymanager.TopologyHint{}
                continue
            }
            klog.InfoS("Regenerating TopologyHints for resource already allocated to pod", "resourceName", resource, "pod", klog.KObj(pod), "allocated", allocated.Len())
            deviceHints[resource] = m.generateDeviceTopologyHints(resource, allocated, sets.Set[string]{}, requested)
            continue
        }

        // Get the list of available devices, for which TopologyHints should be generated.
        available := m.getAvailableDevices(resource)
        if available.Len() < requested {
            klog.InfoS("Unable to generate topology hints: requested number of devices unavailable", "resourceName", resource, "pod", klog.KObj(pod), "request", requested, "available", available.Len())
            deviceHints[resource] = []topologymanager.TopologyHint{}
            continue
        }

        // Generate TopologyHints for this resource given the current
        // request size and the list of available devices.
        deviceHints[resource] = m.generateDeviceTopologyHints(resource, available, sets.Set[string]{}, requested)
    }

    return deviceHints
}
```

我们来简单的分析下

- 定义deviceHints
- 获取到pod的device请求，存入accumulatedResourceRequests中。注意，这时候我们并没有分配具体的device，只是看到pod请求了一个或多个device
- 循环resource和requested，即名称和数量。注意，这里的resource还可以包含其它的device，比如kubevirt的tun设备。
  - 根据resource查找该类型的resource是否包含topo的定义，如果没有，则忽略，并且返回nil。如果有，则aligned=true 。(其实到这一步我们已经进行不下去了，因为没有topo信息被上传)
  - 获取到已经分配的存入allocated变量，如cpumanager一样，pod是可以本地升级的。
  - 如果allocated的的数量>0，证明这个pod其实是一个已经存在的，现在只不过是要重建它。
    - 如果allocated不等于requested，则直接为资源赋值一个空的hints
    - 如果等于，则按照当前allocated直接生成hints。(两个判断都会直接跳出本次循环)
  - 获取到当前资源的可用数量available
    - 如果`available<requested`，那么仍然是空hints，并跳出本次循环
  - 当以上的条件都没有跳出循环，那么就会最后进入hints.

注意，上面有一个是赋值为nil且跳过，而三个条件是赋值为空且跳过。对于一个被定义为包含多个对象的切片来说，nil是可以被添加的。

##### 1.3.2.3.2. generateDeviceTopologyHints方法

deviceManager的generateDeviceTopologyHints函数相对来说就比较简单了，这里请自行查阅。 它的目标也是获取一个topologyHints列表，然后选出preferred的。

#### 1.3.2.4. 再回到Admit方法

注意，这时候又回到了topologyManager中。

##### 1.3.2.4.1. Merge方法

再回到Admit方法，我们会看到多个Manager的Hints返回会被塞入Merge方法中，Merge方法会计算多个hints结果的最优选择。 （这里我们以后再具体分析）

```go
func (s *podScope) calculateAffinity(pod *v1.Pod) (TopologyHint, bool) {
    providersHints := s.accumulateProvidersHints(pod)
    bestHint, admit := s.policy.Merge(providersHints)
    klog.InfoS("PodTopologyHint", "bestHint", bestHint, "pod", klog.KObj(pod))
    return bestHint, admit
}
```

##### 1.3.2.4.2. 存入scope

```go
s.setTopologyHints(string(pod.UID), container.Name, bestHint)
```

```go
type scope struct {
    mutex sync.Mutex
    name  string
    // Mapping of a Pods mapping of Containers and their TopologyHints
    // Indexed by PodUID to ContainerName
    podTopologyHints podTopologyHints
    // The list of components registered with the Manager
    hintProviders []HintProvider
    // Topology Manager Policy
    policy Policy
    // Mapping of (PodUid, ContainerName) to ContainerID for Adding/Removing Pods from PodTopologyHints mapping
    podMap containermap.ContainerMap
}
```

在以上代码中，我们看到了当前Pod的bestHint被存入到了scope的podTopologyHints中。每个pod会对应一个bestHint。

##### 1.3.2.4.3. 获取devices

```go
// err := s.allocateAlignedResources(pod, &container)
func (s *scope) allocateAlignedResources(pod *v1.Pod, container *v1.Container) error {
    for _, provider := range s.hintProviders {
        err := provider.Allocate(pod, container)
        if err != nil {
            return err
        }
    }
    return nil
}
```

allocateAlignedResources又会调用每个子Manager，去调用manager的Allocate方法，来进行真正的分配。这个我们新开一个章节来分析。

#### 1.3.2.5. 总结

这里我们知道了是topology的计算逻辑，并且为device设备请求时进行bestHint的选择，以求达到numa与设备的对应，以提高性能。

### 1.3.3. 分配

在topologyManager的方法中，我们可以看到最后调用allocateAlignedResources方法，然后该方法又去每个子Manager执行Allocate方法。

- q：这时候device被分配了吗？
  - 分了，但没有完全分。 以gpu为例，我们在当前情况下，还是只知道被分配了几个gpu，但并不知道gpu的具体情况。
- q: 多个pod会同时分配吗？
  - 不会，从loop入口我们会看到，它是一个循环执行的机制，如果有多个pod的请求，他们是顺序执行的，完成分配后才会执行下一个的分配。

#### 1.3.3.1. Allocate方法

这里我们以DeviceManager的Allocate方法，来分析分配机制。

```go
func (m *ManagerImpl) Allocate(pod *v1.Pod, container *v1.Container) error {
    if _, ok := m.devicesToReuse[string(pod.UID)]; !ok {
        m.devicesToReuse[string(pod.UID)] = make(map[string]sets.Set[string])
    }
    // If pod entries to m.devicesToReuse other than the current pod exist, delete them.
    for podUID := range m.devicesToReuse {
        if podUID != string(pod.UID) {
            delete(m.devicesToReuse, podUID)
        }
    }
    // Allocate resources for init containers first as we know the caller always loops
    // through init containers before looping through app containers. Should the caller
    // ever change those semantics, this logic will need to be amended.
    for _, initContainer := range pod.Spec.InitContainers {
        if container.Name == initContainer.Name {
            if err := m.allocateContainerResources(pod, container, m.devicesToReuse[string(pod.UID)]); err != nil {
                return err
            }
            if !podutil.IsRestartableInitContainer(&initContainer) {
                m.podDevices.addContainerAllocatedResources(string(pod.UID), container.Name, m.devicesToReuse[string(pod.UID)])
            } else {
                // If the init container is restartable, we need to keep the
                // devices allocated. In other words, we should remove them
                // from the devicesToReuse.
                m.podDevices.removeContainerAllocatedResources(string(pod.UID), container.Name, m.devicesToReuse[string(pod.UID)])
            }
            return nil
        }
    }
    if err := m.allocateContainerResources(pod, container, m.devicesToReuse[string(pod.UID)]); err != nil {
        return err
    }
    m.podDevices.removeContainerAllocatedResources(string(pod.UID), container.Name, m.devicesToReuse[string(pod.UID)])
    return nil
    }
```

如果是以单纯的kubevirt pod来分析, 它会直接进入最后一个allocateContainerResources方法。

注： initContainer机制我们以后再说，目前kubevirt的pod是没有initContainer机制的。

##### 1.3.3.1.1. allocateContainerResources

```go
func (m *ManagerImpl) allocateContainerResources(pod *v1.Pod, container *v1.Container, devicesToReuse map[string]sets.Set[string]) error {
    podUID := string(pod.UID)
    contName := container.Name
    allocatedDevicesUpdated := false
    needsUpdateCheckpoint := false
    // Extended resources are not allowed to be overcommitted.
    // Since device plugin advertises extended resources,
    // therefore Requests must be equal to Limits and iterating
    // over the Limits should be sufficient.
    for k, v := range container.Resources.Limits {
        resource := string(k)
        needed := int(v.Value())
        klog.V(3).InfoS("Looking for needed resources", "resourceName", resource, "pod", klog.KObj(pod), "containerName", container.Name, "needed", needed)
        if !m.isDevicePluginResource(resource) {
            continue
        }
        // Updates allocatedDevices to garbage collect any stranded resources
        // before doing the device plugin allocation.
        if !allocatedDevicesUpdated {
            m.UpdateAllocatedDevices()
            allocatedDevicesUpdated = true
        }
        allocDevices, err := m.devicesToAllocate(podUID, contName, resource, needed, devicesToReuse[resource])
        if err != nil {
            return err
        }
        if allocDevices == nil || len(allocDevices) <= 0 {
            continue
        }

        needsUpdateCheckpoint = true

        startRPCTime := time.Now()

        m.mutex.Lock()
        eI, ok := m.endpoints[resource]
        m.mutex.Unlock()
        if !ok {
            m.mutex.Lock()
            m.allocatedDevices = m.podDevices.devices()
            m.mutex.Unlock()
            return fmt.Errorf("unknown Device Plugin %s", resource)
        }

        devs := allocDevices.UnsortedList()
        // TODO: refactor this part of code to just append a ContainerAllocationRequest
        // in a passed in AllocateRequest pointer, and issues a single Allocate call per pod.
        klog.V(4).InfoS("Making allocation request for device plugin", "devices", devs, "resourceName", resource, "pod", klog.KObj(pod), "containerName", container.Name)
        resp, err := eI.e.allocate(devs)
        metrics.DevicePluginAllocationDuration.WithLabelValues(resource).Observe(metrics.SinceInSeconds(startRPCTime))
        if err != nil {
            // In case of allocation failure, we want to restore m.allocatedDevices
            // to the actual allocated state from m.podDevices.
            m.mutex.Lock()
            m.allocatedDevices = m.podDevices.devices()
            m.mutex.Unlock()
            return err
        }

        if len(resp.ContainerResponses) == 0 {
            return fmt.Errorf("no containers return in allocation response %v", resp)
        }

        allocDevicesWithNUMA := checkpoint.NewDevicesPerNUMA()
        // Update internal cached podDevices state.
        m.mutex.Lock()
        for dev := range allocDevices {
            if m.allDevices[resource][dev].Topology == nil || len(m.allDevices[resource][dev].Topology.Nodes) == 0 {
                allocDevicesWithNUMA[nodeWithoutTopology] = append(allocDevicesWithNUMA[nodeWithoutTopology], dev)
                continue
            }
            for idx := range m.allDevices[resource][dev].Topology.Nodes {
                node := m.allDevices[resource][dev].Topology.Nodes[idx]
                allocDevicesWithNUMA[node.ID] = append(allocDevicesWithNUMA[node.ID], dev)
            }
        }
        m.mutex.Unlock()
        m.podDevices.insert(podUID, contName, resource, allocDevicesWithNUMA, resp.ContainerResponses[0])
    }

    if needsUpdateCheckpoint {
        return m.writeCheckpoint()
    }

    return nil
}
```

- 获取 needed=require，也就是请求的数量
- 如果当前Pod+Container有没有已经存在的设备（这主要是pod升级用或kubelet重启后会进入），在新建的情况下不会进入，所以needed不会变。
- healthyDevices, hasRegistered两个变量被定义，也不需要关心，因为pci-passthrough模式下没有unhealthy的情况。
- 定义一个allocated，来存储已经被分配
- 定义一个allocateRemainingFrom变量为方法，当前不会执行，后面会用到。
- 调用一次allocateRemainingFrom，如果当前pod有请求能重复使用的资源。（这个不会用到，以后再分析）
- devicesInUse为当前kubelet内存中记录的已经被使用的。
- available为当前使用的。
- filterByAffinity才是真正在的分配方法, aligned根据当前pod的bestHint中的numa选出的device, unaligned为不是bestHint中的numa选出的device，而noAffinity则是没有Numa的device
- 如果`needed < aligned`, 那么通过一系列的算法，得出分配的设备。

这里有点乱，但实际上也不乱。 因为我们当前的所有的amd节点都是一个numa对应一个gpu的，那到了intel就不会了，intel可能会一个numa对应多个gpu，所以上面的代码中做了很多判断。

还有一种情况是tun设备，tun设备是一种高性的虚拟网卡，通常在网卡硬件卸载时出现，也可能出现一个numa对应n个tun设备的现象。 当有多块Pci网卡时也可能出现。

##### 1.3.3.1.2. filterByAffinity

这个方法其实会解答我们的疑惑： 即是怎么通过Numa找到设备的。

```go
hint := m.topologyAffinityStore.GetAffinity(podUID, contName)

```

在方法的第一行我们就会看到获取hint，还记得我们在[containerManager对象]那一小节所说吗？ 3个子Manager其实是会拿到topology的scope对象并存储的。而在Admit方法中，我们看到了bestHint在生成后会存入到scope自己的变量中。 那么一切都说通了。

#### 1.3.3.2. 总结

在这里我们知道了bestHint是如何保证拿到numa节点相应的设备的。

## 1.4. 调整

### 1.4.1. pci-passthrough

在上一章节中，我们分析了numa的选择机制，但在kubevirt pod生成过程中，并没有看到关于gpu的numa匹配。经过分析后，我们发现是kubevirt-gpu-device-plugin这个组件并没有上报自己的gpu拓扑信息。

#### 1.4.1.1. 将topology上传上去

我们在分析devicePlugin的源码时，我们说到了topology的获取它就走不下去了，也就是gpu不会有numa_hint，原因就是因为当前daemon它根据没有上传topology上去。

还是以源码为例，来分析它为什么没有上去。

```go
func createDevicePlugins() {
    var devicePlugins []*GenericDevicePlugin
    var vGpuDevicePlugins []*GenericVGpuDevicePlugin
    var devs []*pluginapi.Device
    log.Printf("Iommu Map %s", iommuMap)
    log.Printf("Device Map %s", deviceMap)
    // log.Printf("Iommu node Map %s", iommuNodeMap)
    log.Println("vGPU Map ", vGpuMap)
    log.Println("GPU vGPU Map ", gpuVgpuMap)

    //Iterate over deivceMap to create device plugin for each type of GPU on the host
    for k, v := range deviceMap {
        devs = nil
        for _, dev := range v {
            devs = append(devs, &pluginapi.Device{
                ID:     dev,
                Health: pluginapi.Healthy,
            })
        }
        deviceName := getDeviceName(k)
        if deviceName == "" {
            log.Printf("Error: Could not find device name for device id: %s", k)
            deviceName = k
        }
        log.Printf("DP Name %s", deviceName)
        dp := NewGenericDevicePlugin(deviceName, "/dev/vfio/", devs)
        err := startDevicePlugin(dp)
        if err != nil {
            log.Printf("Error starting %s device plugin: %v", dp.deviceName, err)
        } else {
            devicePlugins = append(devicePlugins, dp)
        }
    }
    //Iterate over vGpuMap to create device plugin for each type of vGPU on the host
    for k, v := range vGpuMap {
        devs = nil
        for _, dev := range v {
            devs = append(devs, &pluginapi.Device{
                ID:     dev.addr,
                Health: pluginapi.Healthy,
            })
        }
        deviceName := getDeviceName(k)
        if deviceName == "" {
            deviceName = k
        }
        log.Printf("DP Name %s", deviceName)
        dp := NewGenericVGpuDevicePlugin(deviceName, vGpuBasePath, devs)
        err := startVgpuDevicePlugin(dp)
        if err != nil {
            log.Printf("Error starting %s device plugin: %v", dp.deviceName, err)
        } else {
            vGpuDevicePlugins = append(vGpuDevicePlugins, dp)
        }
    }

    <-stop
    log.Printf("Shutting down device plugin controller")
    for _, v := range devicePlugins {
        v.Stop()
    }

    for _, v := range vGpuDevicePlugins {
        v.Stop()
    }

}

```

- 在该函数之外，会定义四个全局变量iommuMap、deviceMap和vGpuMap、gpuVgpuMap， 其中前两个是我们需要关心的，它们由createIommuDeviceMap函数来进行初始化，这里没有贴出。

我们以python语法来解析下iommuMap和deviceMap中包含了什么。

```python
# 2204为3090显卡唯一产品号，而列表中的则是每个显卡对应的iommu的id，一共的8个，表明有8块显卡。
# 一般来说,iommu_id与numa没有对应关系，但对于gpu来说，它有一定的对应关系。
iommuMap={2204: [30,49,13,2,96,112,79,68] }

# 从下面可以看出，iommu值的大小与其所携带的显卡的位置也没有什么强关联。
deviceMap= {"2204":{
    112：[0000:a1:00.0, 0000:a1:00.1],
    13: [0000:41:00.0, 0000:41:00.1],
    2: [0000:61:00.0,0000:61:00.1],
    ...共8组
}
}
```

- 那这样的话，其实对deviceMap的循环就比较好理解了，它会根据deviceName为每组device生成一个grpc程序。该程序有两个功能
  - 作为客户端不断向kubelet上报自己的gpu的信息，包括unhealthy的gpu，但对于vfio驱动的gpu unhealthy没有什么意义。
  - 也会作为服务端实现Allocate接口，等待kubelet的Allocate的调用。

#### 1.4.1.2. 上报的device信息

我们在上一小节的循环中可以看到，它使用DeviceMap生成了一个新的包含多个Device对象的devs变量。但从一列代码中，我们可以看到有Topology的定义，Topology中也包含Nedes信息。

```go
type Device struct {
    // A unique ID assigned by the device plugin used
    // to identify devices during the communication
    // Max length of this field is 63 characters
    ID string `protobuf:"bytes,1,opt,name=ID,proto3" json:"ID,omitempty"`
    // Health of the device, can be healthy or unhealthy, see constants.go
    Health string `protobuf:"bytes,2,opt,name=health,proto3" json:"health,omitempty"`
    // Topology for device
    Topology             *TopologyInfo `protobuf:"bytes,3,opt,name=topology,proto3" json:"topology,omitempty"`
    XXX_NoUnkeyedLiteral struct{}      `json:"-"`
    XXX_sizecache        int32         `json:"-"`
}

type TopologyInfo struct {
    Nodes                []*NUMANode `protobuf:"bytes,1,rep,name=nodes,proto3" json:"nodes,omitempty"`
    XXX_NoUnkeyedLiteral struct{}    `json:"-"`
    XXX_sizecache        int32       `json:"-"`
}

type NUMANode struct {
    ID                   int64    `protobuf:"varint,1,opt,name=ID,proto3" json:"ID,omitempty"`
    XXX_NoUnkeyedLiteral struct{} `json:"-"`
    XXX_sizecache        int32    `json:"-"`
}
```

我们需要修改源码，使得其能将这个node信息上传上去。

---

首先，我们需要定义一个iommuNodeMap,在createIommuDeviceMap方法时，将iommuNodeMap也给赋值，最后iommuNodeMap会如下。(具体代码这里不贴出了)

```go

// 从pci设备中的路径中读取到numa_node. 如/sys/bus/pci/devices/0000:a1:00.0/numa_node
// node, err := readNumaFromFile(basePath, info.Name(), "numa_node")
// 将其填入到map字典中，如果有显卡和声卡的node一定会相同，map也有去除的功能。
// iommuNodeMap[iommuGroup] = node
// 最后形成这样一个对应关系表的Map
// iommuNodeMap={49:0,2:1,....}

```

---
修改devs代码

```go
// 修改前
for _, dev := range v {
    devs = append(devs, &pluginapi.Device{
        ID:     dev,
        Health: pluginapi.Healthy,
    })
}

// 修改后
for _, dev := range v {
    pluginDevice := &pluginapi.Device{
        ID:     dev,
        Health: pluginapi.Healthy,
    }
    node, exist := iommuNodeMap[dev]
    if exist {
        nodeID, _ := strconv.ParseInt(node, 10, 64)
        pluginDevice.Topology = &pluginapi.TopologyInfo{
            Nodes: []*pluginapi.NUMANode{
                {
                    ID: nodeID,
                },
            },
        } 
    }
    devs = append(devs, pluginDevice)
}
```

编译main.go ，然后将其覆盖到container镜像。 服务器端删除原有镜像再删除运行的daemon，达到重启的目的。

#### 1.4.1.3. 测试结果

目前测试来看topology已经生效，pod中的cpuset和pci的设备已经对应起来。 但虚拟机方面仍然没有对应，这在openstack中我们也遇到过，后来是通过修改nova-compute的源码进行的解决。看来我们也需要修改kubevirt的源码来解决了。

### 1.4.2. containerd-runc

从目前来看，containerd-runc 模式下可以正常的进行numa匹配, 只需要和vm-passthrough进行一样的配置即可：

示例pod设置

```yaml
apiVersion: v1
kind: Pod
metadata:
  name: gpu-pod
spec:
  containers:
    - name: gpu-container
      image: nvidia/cuda:12.0.0-base-ubuntu20.04
      command: ["sleep", "infinity"]
      resources:
        limits:
          nvidia.com/gpu: 2 
          cpu: 22
          memory: 16Gi
        requests:
          nvidia.com/gpu: 2 
          cpu: 22
          memory: 16Gi
  restartPolicy: Always
```

## 1.5. 内存分配

[内存分配](https://kubernetes.io/docs/tasks/administer-cluster/memory-manager)

问题1: 内存有单独的numa匹配机制, 如果开启了cpu static分配而不开启memory static分配,就会产生一些问题,最大的可能是内存溢出,导致oom kill的产生.

问题2: 但开启后也会产生问题,因为我们在上面讨论了,每个子manager都会有自己的Hints算法, 而memory也会有自己的Hints算法, 这时候就会产生大的问题.

这里我们只说pod级别的分配.

### 1.5.1. 源码解读

```go
func (p *staticPolicy) calculateHints(machineState state.NUMANodeMap, pod *v1.Pod, requestedResources map[v1.ResourceName]uint64) map[string][]topologymanager.TopologyHint {
    var numaNodes []int
    for n := range machineState {
        numaNodes = append(numaNodes, n)
    }
    sort.Ints(numaNodes)

    // Initialize minAffinitySize to include all NUMA Cells.
    minAffinitySize := len(numaNodes)

    hints := map[string][]topologymanager.TopologyHint{}
    bitmask.IterateBitMasks(numaNodes, func(mask bitmask.BitMask) {
        maskBits := mask.GetBits()
        singleNUMAHint := len(maskBits) == 1

        totalFreeSize := map[v1.ResourceName]uint64{}
        totalAllocatableSize := map[v1.ResourceName]uint64{}
        // calculate total free and allocatable memory for the node mask
        for _, nodeID := range maskBits {
            for resourceName := range requestedResources {
                if _, ok := totalFreeSize[resourceName]; !ok {
                    totalFreeSize[resourceName] = 0
                }
                totalFreeSize[resourceName] += machineState[nodeID].MemoryMap[resourceName].Free

                if _, ok := totalAllocatableSize[resourceName]; !ok {
                    totalAllocatableSize[resourceName] = 0
                }
                totalAllocatableSize[resourceName] += machineState[nodeID].MemoryMap[resourceName].Allocatable
            }
        }

        // verify that for all memory types the node mask has enough allocatable resources
        for resourceName, requestedSize := range requestedResources {
            if totalAllocatableSize[resourceName] < requestedSize {
                return
            }
        }

        // set the minimum amount of NUMA nodes that can satisfy the container resources requests
        if mask.Count() < minAffinitySize {
            minAffinitySize = mask.Count()
        }

        // the node already in group with another node, it can not be used for the single NUMA node allocation
        if singleNUMAHint && len(machineState[maskBits[0]].Cells) > 1 {
            return
        }

        for _, nodeID := range maskBits {
            // the node already used for the memory allocation
            if !singleNUMAHint && machineState[nodeID].NumberOfAssignments > 0 {
                // the node used for the single NUMA memory allocation, it can not be used for the multi NUMA node allocation
                if len(machineState[nodeID].Cells) == 1 {
                    return
                }

                // the node already used with different group of nodes, it can not be use with in the current hint
                if !areGroupsEqual(machineState[nodeID].Cells, maskBits) {
                    return
                }
            }
        }

        // verify that for all memory types the node mask has enough free resources
        for resourceName, requestedSize := range requestedResources {
            podReusableMemory := p.getPodReusableMemory(pod, mask, resourceName)
            if totalFreeSize[resourceName]+podReusableMemory < requestedSize {
                return
            }
        }

        // add the node mask as topology hint for all memory types
        for resourceName := range requestedResources {
            if _, ok := hints[string(resourceName)]; !ok {
                hints[string(resourceName)] = []topologymanager.TopologyHint{}
            }
            hints[string(resourceName)] = append(hints[string(resourceName)], topologymanager.TopologyHint{
                NUMANodeAffinity: mask,
                Preferred:        false,
            })
        }
    })

    // update hints preferred according to multiNUMAGroups, in case when it wasn't provided, the default
    // behaviour to prefer the minimal amount of NUMA nodes will be used
    for resourceName := range requestedResources {
        for i, hint := range hints[string(resourceName)] {
            hints[string(resourceName)][i].Preferred = p.isHintPreferred(hint.NUMANodeAffinity.GetBits(), minAffinitySize)
        }
    }

    return hints
}
```

以上代码仍然沿用了cpuManager中的逻辑, 其实中的minAffinitySize为最大的问题, 如果一个node的内存剩余满足了条件,那么就会产生:

- cpuManager选择了node0/node1两个node,因为它需要两个node才能满足内核数量的请求.
- 但memoryManager却只选择了node0,因为一个内存节点就可以满足内存的请求了.
- 那么结果就会导致cpuManager和memoryManager无法计算出有效集.

结果就是我们必须强行进行内存的与cpu的对应,以保证选出的cpu与内存的numa集一致

#### 1.5.1.1. 不使用static模式

不使用static模式会产生什么?

参考文档 <https://zhuanlan.zhihu.com/p/554397630>

在不使用static模式的情况下,就会由操作系统采用就近分配的形式来解决, 这种情况在本节点内存充足的情况下还是可以的, 经过测试不会出现remote分配的情况.

#### 1.5.1.2. 为什么会oom kill

答:超出内存的限制了. (废话)

我们stress用错了,组合错误导致超出给pod分配的内存, 但为什么pod会发生oom kill呢? 通常情况下我们认为一个pod内的进程内存超出不应该杀pod啊, 应该杀的是进程啊.

<https://izsk.me/2023/02/09/Kubernetes-Out-Of-Memory-1/>  <https://blog.csdn.net/ygq13572549874/article/details/144357185>  <https://kubernetes.io/zh-cn/docs/tasks/configure-pod-container/assign-memory-resource/>

通过上面的文章我们可以大概有以下的解释:

- 我们的每个pd都会有自己的单独的cgroup组, 而cgroup组是没有oom kill的能力的,得依赖于操作系统.
- 当cgroup组整体超过其设置的limit时，内核首先会尝试从cgroup内部回收内存，如果回收不成功，将调用OOM程序来选择(打分)并终止cgroup内最庞大的任务.
- 一旦突破了cgroup组超过limit，oom-killer一定会选择一个进程kill掉，但是被Kill掉的进程不一定是我们主客认为的最有可能被Kill的那个进程

我们来看一下我们的系统日志:

```
[Tue Mar 11 10:16:36 2025] oom-kill:constraint=CONSTRAINT_MEMCG,nodemask=(null),cpuset=cri-containerd-85d3a7f5db9b92df4f5930b5bf062cff540084aacfc05310acc6c5cc963023ad.scope,mems_allowed=0-7,oom_memcg=/kubepods.slice/kubepods-podf9411888_a753_41f4_9520_afc45712f5b5.slice,task_memcg=/kubepods.slice/kubepods-podf9411888_a753_41f4_9520_afc45712f5b5.slice/cri-containerd-85d3a7f5db9b92df4f5930b5bf062cff540084aacfc05310acc6c5cc963023ad.scope,task=stress,pid=3804874,uid=0
[Tue Mar 11 10:16:36 2025] Memory cgroup out of memory: Killed process 3804874 (stress) total-vm:13632272kB, anon-rss:1489488kB, file-rss:264kB, shmem-rss:0kB, UID:0 pgtables:2956kB oom_score_adj:-997
[Tue Mar 11 10:16:36 2025] Tasks in /kubepods.slice/kubepods-podf9411888_a753_41f4_9520_afc45712f5b5.slice/cri-containerd-85d3a7f5db9b92df4f5930b5bf062cff540084aacfc05310acc6c5cc963023ad.scope are going to be killed due to memory.oom.group set
[Tue Mar 11 10:16:36 2025] Memory cgroup out of memory: Killed process 3803554 (sleep) total-vm:1572kB, anon-rss:4kB, file-rss:0kB, shmem-rss:0kB, UID:0 pgtables:32kB oom_score_adj:-997
[Tue Mar 11 10:16:36 2025] Memory cgroup out of memory: Killed process 3803846 (bash) total-vm:2416kB, anon-rss:436kB, file-rss:1632kB, shmem-rss:0kB, UID:0 pgtables:44kB oom_score_adj:-997
[Tue Mar 11 10:16:36 2025] Memory cgroup out of memory: Killed process 3804873 (stress) total-vm:13632272kB, anon-rss:1258752kB, file-rss:264kB, shmem-rss:0kB, UID:0 pgtables:2508kB oom_score_adj:-997
[Tue Mar 11 10:16:36 2025] Memory cgroup out of memory: Killed process 3804874 (stress) total-vm:13632272kB, anon-rss:1489488kB, file-rss:264kB, shmem-rss:0kB, UID:0 pgtables:2956kB oom_score_adj:-997
[Tue Mar 11 10:16:36 2025] Memory cgroup out of memory: Killed process 3804875 (stress) total-vm:13632272kB, anon-rss:992376kB, file-rss:264kB, shmem-rss:0kB, UID:0 pgtables:1984kB oom_score_adj:-997
[Tue Mar 11 10:16:36 2025] Memory cgroup out of memory: Killed process 3804876 (stress) total-vm:13632272kB, anon-rss:896808kB, file-rss:264kB, shmem-rss:0kB, UID:0 pgtables:1796kB oom_score_adj:-997
[Tue Mar 11 10:16:36 2025] Memory cgroup out of memory: OOM victim 3804877 (stress) is already exiting. Skip killing the task
[Tue Mar 11 10:16:36 2025] Memory cgroup out of memory: OOM victim 3804878 (stress) is already exiting. Skip killing the task
[Tue Mar 11 10:16:36 2025] Memory cgroup out of memory: Killed process 3804879 (stress) total-vm:13632272kB, anon-rss:1162128kB, file-rss:264kB, shmem-rss:0kB, UID:0 pgtables:2320kB oom_score_adj:-997
[Tue Mar 11 10:16:36 2025] Memory cgroup out of memory: Killed process 3804880 (stress) total-vm:13632272kB, anon-rss:1112496kB, file-rss:264kB, shmem-rss:0kB, UID:0 pgtables:2220kB oom_score_adj:-997
[Tue Mar 11 10:16:36 2025] Memory cgroup out of memory: Killed process 3804881 (stress) total-vm:13632272kB, anon-rss:1034616kB, file-rss:264kB, shmem-rss:0kB, UID:0 pgtables:2068kB oom_score_adj:-997
[Tue Mar 11 10:16:36 2025] Memory cgroup out of memory: OOM victim 3804882 (stress) is already exiting. Skip killing the task
[Tue Mar 11 10:16:36 2025] Memory cgroup out of memory: Killed process 3804883 (stress) total-vm:13632272kB, anon-rss:1034352kB, file-rss:264kB, shmem-rss:0kB, UID:0 pgtables:2068kB oom_score_adj:-997
[Tue Mar 11 10:16:36 2025] Memory cgroup out of memory: OOM victim 3804884 (stress) is already exiting. Skip killing the task
[Tue Mar 11 10:16:36 2025] Memory cgroup out of memory: OOM victim 3804885 (stress) is already exiting. Skip killing the task
[Tue Mar 11 10:16:36 2025] Memory cgroup out of memory: OOM victim 3804886 (stress) is already exiting. Skip killing the task
[Tue Mar 11 10:16:36 2025] Memory cgroup out of memory: OOM victim 3804887 (stress) is already exiting. Skip killing the task
```

我们会发现一个问题, oom-kill会计算pod中各个进程的score值来决定谁会被杀掉, 但oom-kill也有些傻, 我们说系统的oom-kill是绝对不会杀死进程1的. 但cgroup中的进程可不是这样的, 对于系统的oom-kill来说, 它看到cgroup中的pid都是系统内的id. 也就是说cgroup中为1的进程在系统内可能就是一个别的进程号, 比如我们的sleep进程在我们的cgroup中是进程1,但在系统内是3803554.

oom-kill的另一个傻的地方就是它只算分数和pid号,不算内存的使用情况, 根据你们的pid号"排头砍去",所以就导致了我们sleep进程被杀,进而也就导致了整个pod被杀掉.

所以说to kill or not to kill , 这是一个问题:

- 如果一个pod不做限制, 就算它独占一整个节点,它就有可能将整个节点跑死
- 做了限制,那么它就有被kill的风险.
- 给它score提高呢? 给它做限制的同时再给整个pod的score提高,会怎么样呢?

虽然参考文档里面提供了下面这个示例,但我们没有在代码中找到pod可以使用oomScoreAdj这样参数,但确实可以为当前kubelet设置达到全局的效果.

```
apiVersion: v1
kind: Pod
metadata:
  name: mypod
spec:
  securityContext:
    oomScoreAdj: 500
  containers:
  - name: mycontainer
    image: myimage
```

#### pod中程序对cgroup的使用

<https://simplealgo.com/jvm-vs-pvm/>

我们说高级语言都可以进行自动的内存管理（c++???），做AI的标准语言们python和java的都是有自动的内存回收策略的，正常情况下是不会发生oom-kill的问题的。 而我们测试的stress是采用c来写的，所以能挤爆内存。

### 1.5.2. kubevirt的内存匹配

参考文档: <https://blog.csdn.net/NUCEMLS/article/details/131797992>

kubevirt在使用Numa的情况下,是强行的进行大页内存的使用的. 这个不清楚是机制的问题还是什么. 并且如上面的文档, 需要注意的是一个kubevirt为pod本身保留内存的一个问题,因为大页都给虚拟机用了,但pod本身仍然是需要内存的,这种情况需要考虑下内存保留的问题,但问题不大.

#### 1.5.2.1. hugePages

大页内存可以部分提升内存性能

## 1.6. cpu分配

### 1.6.1. cpu均匀的分配

<https://kubernetes.io/zh-cn/blog/2024/08/22/cpumanager-static-policy-distributed-cpu-across-cores/>

在k3s中的配置应该做如下配置

```yaml
kubelet-arg:
- runtime-request-timeout=15m
- container-log-max-files=3
- container-log-max-size=10Mi
# cpu支持静态分配
- cpu-manager-policy=static
# cpu与numa严格约束
- topology-manager-policy=restricted
# 保留的cpu
- reserved-cpus=0,6,12,18,24,30,36,42
# 为k8s开启CPUManagerPolicyAlphaOptions这个特性
- feature-gates=CPUManagerPolicyAlphaOptions=true
# 只有开了CPUManagerPolicyAlphaOptions特性,本条才会成立. 使得cpu可以均匀的分配到多个numa上. 
- cpu-manager-policy-options=distribute-cpus-across-numa=true
```

- q : 开启distribute-cpus-across-numa后cpu分配绝对均匀吗?
  - a: 当然不是,无论如何11个cpu无法远的的分配到两个numa节点上
- q: 那可以均匀分配后是不是可以考虑减少reserved-cpus
  - a: 是的,可以减少reserved-cpus的使用,使得留出更多的cpu资源可以分配给pod使用,但这并不绝对,还是需要综合考虑的.
- q: 还有什么
  - a: cpuManager和topologyManager其实也是feature,只不过他们的重要性使得他们是默认开启的(ps:至少在k3s中是默认开启了)

## 1.7. 一些代码路径

### 1.7.1. features配置

代码路径: `/kubernetes/pkg/features/kube_features.go`. 通过对CPUManagerPolicyAlphaOptions的配置,我们找到了该文件,这里面提供了一个相当全面的feature的配置选项.

### 1.7.2. policy配置

`distribute-cpus-across-numa`为CPUManagerPolicyAlphaOptions的一个policy,通常会在每个manager的`policy_options.go`中, 如`/kubernetes/pkg/kubelet/cm/cpumanager/policy_options.go`

## 1.8. 最后

### 1.8.1. kubenet配置文件

```yaml
kubelet-arg:
- runtime-request-timeout=15m
- container-log-max-files=3
- container-log-max-size=10Mi  
- cpu-manager-policy=static
- topology-manager-policy=restricted
- reserved-cpus=0,6,12,18,24,30,36,42
- feature-gates=CPUManagerPolicyAlphaOptions=true
- cpu-manager-policy-options=distribute-cpus-across-numa=true
  #- reserved-cpus=4
  #- memory-manager-policy=Static
  #- system-reserved=memory=4Gi
  #- kube-reserved=memory=4Gi
  #- reserved-memory=0:memory=1Gi;1:memory=1Gi;2:memory=1Gi;3:memory=1Gi;4:memory=1Gi;5:memory=1Gi;6:memory=1Gi;7:memory=1Gi
```

### 1.8.2. 节点模式切换

模式切换相对来说比较简单，只要设置一个节点是vfio驱动还是nvidia驱动即可，在Kubelet的配置上他们是一致的。当设置完成后gpu-operator会自动进行daemonset的转换。

### 1.8.3. 大页设置

kubevirt需要大页而pod不需要大页,我们可以参考gpu-operator的操作, 在模式切换时进行大页的初始化或删除.
