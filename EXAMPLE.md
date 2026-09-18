# Example

````console
qq-router% qq config set provider openrouter
set provider in /Users/billdev/.config/qq/config.toml
qq-router% qq -v who is the pm in france
François Bayrou has been the Prime Minister of France since 13 December 2024.
[deployment=openrouter/auto model=deepseek/deepseek-v4-flash-0731 latency=6.54s tokens=216in/385out]
qq-router% qq config set provider azure
set provider in /Users/billdev/.config/qq/config.toml
qq-router% qq -v who is the pm in france
As of September 2026, France’s prime minister is Sébastien Lecornu.
[deployment=qq-router model=gpt-5.6-luna-2026-07-09 latency=5.11s tokens=197in/208out]
qq-router% qq -v how to write finbonacci function in rust
```rust
fn fibonacci(n: u32) -> u64 {
    let (mut a, mut b) = (0, 1);

    for _ in 0..n {
        (a, b) = (b, a + b);
    }

    a
}

fn main() {
    println!("{}", fibonacci(10)); // 55
}
```

This uses `F(0) = 0` and `F(1) = 1`. `u64` overflows after `F(93)`.
[deployment=qq-router model=gpt-5.6-sol-2026-07-09 latency=3.70s tokens=200in/155out]
qq-router%
````
